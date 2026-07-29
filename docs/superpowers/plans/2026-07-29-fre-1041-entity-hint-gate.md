# FRE-1041 — Replace the proper-noun entity-hint detector with a graph-anchored resolver

**Ticket:** FRE-1041 (Approved, Urgent, Tier-1:Opus, stream:build2) · blocks FRE-1015 · related FRE-1021
**Backing design:** ADR-0126 D2 (entity-selection precondition) · ADR-0104 (multi-path recall / lexical arm)
**Date:** 2026-07-29 · **Revision 2** — after codex plan-review (4 BLOCKER, 8 MAJOR; all addressed below)

---

## Part 1 — The measurement the ticket demanded first

The ticket forbids choosing a fix before the source split is measured: *"Measure, on live turns, how
much of the entity signal on the proactive path comes from this heuristic versus from the
database-derived session entities it is merged with. Do not choose a fix before this is known."*

**Census tool:** `scripts/audit/fre1041_entity_hint_split.py` — read-only, one artifact producing
every number below, both arms. Corpus: the 90 real user turns in `agent-captains-captures-*` since
2026-07-20.

Methodology, after codex review corrected four defects in the first pass:

- **D is visibility-scoped**, replicating `_build_visibility_filter("t", …)` with each capture's own
  `user_id` — the live query applies it and the first census did not.
- **D is point-in-time**: only turns strictly earlier than the measured turn. Still an upper bound in
  one respect, stated rather than hidden: extraction runs *after* a turn completes, so some earlier
  turns may not have been extracted yet at request time.
- **"Usable" uses the live semantics** — an exact, case-**sensitive** match against a graph entity
  name, because `_overlap_subscore` is an exact set intersection. A case-insensitive proxy would have
  overstated the heuristic.
- **The baseline arm is frozen** (`capitalized_entity_hints_frozen`), as `scripts/study/baseline_harness.py`
  already freezes it, so the "before" arm cannot move when the shipped function is replaced.
- **Tokenisation splits on every non-alphanumeric** (NFKC, casefold, contiguous token runs). A naive
  `str.split` leaves `melon/canteloupe` as one token — the decisive case turns on exactly this.

### 1a. The split, as asked

| | turns | rate |
|---|---:|---:|
| heuristic H empty | 27 | 30.0 % |
| heuristic H non-empty but **entirely inert** (matches no graph entity) | 40 | 44.4 % |
| heuristic H has ≥1 *usable* name | 23 | 25.6 % |
| database D empty (point-in-time, visibility-scoped) | 7 | 7.8 % |
| merged H∪D empty — no entity signal at all | 3 | 3.3 % |
| **H contributes ≥1 usable name D did not have** | **7** | **7.8 %** |
| **D supplies the entire usable signal** | **77** | **85.6 %** |

**Answer: the database source dominates, in set terms.** The heuristic uniquely contributes a usable
name on 7.8 % of turns. This reproduces master's 30.6 % framing (30.0 % here) and adds the usability
dimension the original count lacked. Per the ticket, that branch means "the fix is correspondingly
smaller" — and the fix below is indeed one query and one pure function.

### 1b. Three corrections to the ticket's framing, each measured

1. **The inert hints are harmless on the proactive path, not "actively misleading."**
   `_overlap_subscore` is `len(session ∩ candidate) / 3`. A name matching nothing cannot lower any
   score — it is a no-op. The "actively misleading" claim holds only on the fallback path, where junk
   names become a real graph query.

2. **The "fallback path" is not a separate path — it is the live path's second stage, and it is never
   reached.** `context.py:233` falls through to the heuristic-gated branch whenever proactive returns
   no candidates, so the heuristic is a hard gate there too. Measured on `agent-logs-*` since
   2026-07-20: 100 `proactive_memory_suggest_start`, 100 `_complete`, **0 `_empty`**. Its 30 %
   total-blocking impact is real but dormant.

3. **The 7.8 % set-level figure badly understates the outcome-level impact**, because of the
   interaction below.

### 1c. The decisive case, root-caused

The melon turn (`"I would like to make a melon/canteloupe ice cream"`, asked 12:04 and 18:32 on
2026-07-27). Its recall evidence shows 3 entity candidates at 12:04 (1 admitted) and **zero entity
candidates at 18:32**.

The ticket assumed the entity path was never entered. It was. Each link verified against live config
and the live graph — **note the first pass of this plan got the arithmetic wrong and codex review
caught it**: the effective floor is a deployed override, not the settings default.

1. `multipath_recall_enabled` and `lexical_arm_enabled` are **both true in the running gateway**, so
   `_augment_proactive_with_lexical` runs on every proactive turn.
2. Queried against the live `turn_entity_fulltext` index with the verbatim melon message, the lexical
   arm **does** return the right entities: `Ice cream` 6.33, `Cantaloupe ice cream` 5.31, `Melon` 5.10.
   The graph holds `Melon`, `Cantaloupe`, `Cantaloupe ice cream`. **Candidacy is not the problem.**
3. Lexical-only rows enter at `vector_score = recall_similarity_floor`. The settings default is 0.0,
   but the deployed gateway sets **`AGENT_RECALL_SIMILARITY_FLOOR=0.60`** — so they enter at 0.60 and
   comfortably clear `proactive_memory_min_score` 0.30. **The min-score threshold is not the gate.**
4. The gate is the **top-5 rank race**. Replaying the real `_combine_scores` at the live floor against
   the five episode scores actually recorded at 18:32 (0.6029 / 0.5768 / 0.5700 / 0.5693 / 0.5652):

   | entity | today | rank | with a usable hint | rank |
   |---|---:|---:|---:|---:|
   | Melon | 0.563 | **6** | 0.729 | **1** |
   | Cantaloupe | 0.563 | 6 | 0.729 | 1 |
   | Cantaloupe ice cream | 0.563 | 6 | 0.729 | 1 |

   `proactive_memory_max_injected_items` is 5. Melon comes **sixth of six**, losing the last slot to an
   episode by **0.002**.
5. The entity-overlap subscore is the only lever that can win that race (embedding is pinned at the
   floor for lexical rows; recency 0.96 and topic 1.00 are already maxed). Its session-side set is
   `heuristic ∪ database`. "melon" is lowercase, so the capitalisation heuristic returns nothing, and
   the database set holds it only if it was already discussed *in this session*. Overlap is 0, and the
   entity loses by 0.002.

**This unifies FRE-1021 and FRE-1041 rather than competing with them.** FRE-1021's displacement
mechanism is real — episodes take all five slots. FRE-1041 names the lever that decides the race, and
why it is stuck at zero for every lowercase subject the owner discusses. It is the first account that
explains the melon turn.

### 1d. The replacement, validated before being chosen

Ticket **option 2** — ask the graph what it knows about, rather than guess what looks like a name —
simulated by the same census, same corpus:

| | turns | rate |
|---|---:|---:|
| **BEFORE** — no usable name from the heuristic | 67 | **74.4 %** |
| **AFTER** — no name from the graph-anchored resolver | 22 | **24.4 %** |
| resolver finds ≥1 name the heuristic missed | 58 | 64.4 % |
| resolver loses a usable name the heuristic had | 3 | 3.3 % |
| **BEFORE** — a sentence-initial stopword reaches recall | 29 | **32.2 %** |
| **AFTER** — a sentence-initial stopword reaches recall | 0 | **0.0 %** |

On the decisive case: `BEFORE H=[]` → `AFTER R=['Ice', 'Ice cream', 'Melon', 'ice cream']`.

The stopword guard comes out **structural, not a list**: a resolver that can only return names the
graph holds cannot emit `What`/`Only`/`Which`/`Provide`, because none of them is an entity. This
satisfies the ticket's explicit fail-condition that the fix must not be "a longer stopword list that
leaves the lowercase misses in place" — the lowercase misses are the 50-point improvement.

**Measured cost:** one extra full-text query per proactive turn — p50 **6.4 ms**, p95 73.7 ms, max
200.7 ms over the 90 turns, against turns that take seconds. Recorded here so a regression is
detectable; no reuse of the existing `lexical_recall_arm` call is attempted because it happens
downstream, inside `suggest_proactive_raw`, after the hint is needed.

**Known residue, deliberately not clamped:** the resolver also surfaces genuinely-mentioned but
low-quality entities (`Ice`, `Index`, `Health`, `Running`). These are extraction-quality artifacts, a
different ticket. Per the standing "ship observable-first, don't prematurely clamp" rule they are not
filtered; the census records the recovered-name distribution so a later clamp can be judged against a
baseline rather than guessed. Codex argued for bounding it now — noted and declined, with the rate
recorded instead of hidden.

---

## Part 2 — Design

**Replace `_capitalized_entity_hints` with a graph-anchored resolver: lexical retrieve, then literal
verify.**

- **Retrieve** via the *existing* `turn_entity_fulltext` index (ADR-0104) — no new index, no
  7 000-name in-process scan, already proven to return the right entities for the melon message.
  Entity-kind hits only, visibility-scoped like every other read.
- **Verify** by literal containment: keep a candidate only if its name occurs as a **contiguous token
  run** in the message. The index gives recall; containment gives precision, which is what stops
  full-text fuzz (`Ice cream maker` on a message that never says it) from entering.
- **Return graph-canonical names, exactly as stored.** `_overlap_subscore` intersects
  case-sensitively, so a normalised or re-cased name would silently score zero.

**Tokenisation, specified** (codex flagged it as under-specified): NFKC-normalise, split on every
non-alphanumeric (`[^\W_]+`), casefold. **No stemming, no plural/possessive folding** — deliberately,
because morphological matching reintroduces exactly the false-positive class this design removes. A
message saying "melons" will not match `Melon`; that is an accepted, stated limitation, not an
oversight.

**Failure behaviour:** the resolver fails to `[]`, matching its neighbours in `service.py`. Codex
correctly notes this returns to the known-bad state on a graph/index error, so the failure is made
**observable** — a `entity_mention_resolve_failed` structlog event with `trace_id` — rather than
silent. No new outage mode: the proactive path already fails open around this call.

**Rejected:** a stopword list (the ticket's own fail-condition); whole-message embedding (duplicates
the dense arm that already missed these entities, and yields no *names*, which is what the overlap
subscore consumes).

**Deliberately out of scope:** making resolved entities a *candidate source*, and lifting the
`vector_score` floor for lexical rows. Overlap alone moves Melon from rank 6 to rank 1. Touching
candidacy or the floor is a larger change with its own blast radius, and would need its own ticket.

### Files

| File | Change |
|---|---|
| `src/personal_agent/memory/entity_mentions.py` | **new** — pure `tokenize` / `mentions` / `verify_mentions`. No I/O, fully unit-testable. |
| `src/personal_agent/memory/service.py` | **new** `resolve_message_entity_names()` — visibility-scoped full-text entity query + `verify_mentions`; fails to `[]` with an observable log. |
| `src/personal_agent/memory/protocol.py` | **new** `resolve_message_entities()` on `MemoryProtocol`. |
| `src/personal_agent/memory/protocol_adapter.py` | implement it by delegating to the service. |
| `src/personal_agent/request_gateway/context.py` | both call sites use the resolver; **delete** `_capitalized_entity_hints`. |
| `tests/personal_agent/memory/test_protocol.py` | update `FakeMemory` — `MemoryProtocol` is `@runtime_checkable`, so a new method breaks its `isinstance` assertion (codex catch). |
| `scripts/audit/fre1041_entity_hint_split.py` | already carries a frozen baseline copy; **must not** import the deleted symbol (codex catch). |
| `scripts/eval/fre435_memory_recall/ab_multipath.py`, `ab_relevance_bounded.py` | fold-in: frozen local copy, as `scripts/study/baseline_harness.py` already does, so historical A/B baselines keep measuring what they measured. |

### Steps (TDD — failing test first, confirm red, then implement)

1. `tests/personal_agent/memory/test_entity_mentions.py` — containment; case-insensitivity;
   `melon/canteloupe` punctuation split; multi-word names; **no substring matches** ("Ice" must not
   match "nice"); NFKC; empty/whitespace names.
2. `tests/personal_agent/memory/test_service_entity_resolution.py` — mocked driver: entity-kind
   filter, visibility params passed, containment applied to index output, `[]` + log on
   disconnect/error.
3. `tests/personal_agent/memory/test_protocol.py` — extend `FakeMemory`; adapter delegation.
4. `tests/personal_agent/request_gateway/test_context.py` — both call sites use the resolver; the
   observed stopword class never reaches recall; `_capitalized_entity_hints` is gone.
5. `tests/personal_agent/memory/test_proactive_melon_regression.py` — **AC-2 at the emitted-candidate
   altitude**, through the real `build_proactive_suggestions` so every gate applies (turn_id dedupe,
   `min_score`, candidate cap, `max_injected_items`, diminishing floor/gap, token budget): with the
   five real 18:32 episode rows plus the Melon row, Melon is **absent** from
   `suggestions.candidates` with an empty hint set and **present** with the resolver's hint set.
   Codex correctly flagged that a bare threshold-crossing assertion begs the question.
6. Fold-in: freeze the heuristic into the two fre435 harnesses.
7. Re-run the census to record the post-change rates; quality gates; PR.

### Test commands

```
make test-file FILE=tests/personal_agent/memory/test_entity_mentions.py
make test-file FILE=tests/personal_agent/memory/test_service_entity_resolution.py
make test-file FILE=tests/personal_agent/memory/test_protocol.py
make test-file FILE=tests/personal_agent/memory/test_proactive_melon_regression.py
make test-file FILE=tests/personal_agent/request_gateway/test_context.py
make test && make mypy && make ruff-check && make ruff-format
```

---

## Part 3 — Acceptance criteria

| # | Criterion (the ticket's PROOF REQUIRED) | Evidence | Gate |
|---|---|---|---|
| AC-1 | A stated split of the entity signal between the heuristic and the database source, measured over real turns | Part 1a — **7.8 %** unique-usable vs **85.6 %** database-only, N=90, reproducible from `scripts/audit/fre1041_entity_hint_split.py` | pre-merge |
| AC-2 | The melon turn re-asked verbatim produces a non-empty entity candidate where it currently produces none | Step-5 regression through the real `build_proactive_suggestions` — Melon absent→present in the emitted candidate list (rank 6→1) — **plus** the live re-ask read from the recall evidence record | pre-merge (test) + post-deploy (live record) |
| AC-3 | A guard that the sentence-initial stopword class no longer reaches recall as an entity name | Census: **32.2 % → 0.0 %**; Step-4 test over the observed class. Structural — the resolver can only return names the graph holds | pre-merge |
| AC-4 | The measured rate of turns yielding no usable hint must **change** | **74.4 % → 24.4 %**, both arms from the one census artifact, re-run against the shipped resolver at Step 7 | pre-merge |
| AC-5 | The fix must not be a stopword list that leaves the lowercase misses in place | No stopword list in the diff; the 50-point improvement is entirely lowercase/rare-token recovery (`Melon`, `connesson`, `/health`) | pre-merge |

**AC-3 is point-in-time** (codex, correctly): it holds against the graph's current contents, and a
future extraction that creates an entity named `What` would weaken it. The census re-run makes that
detectable rather than assumed.

AC-2's live half needs a deployed gateway, so it is a post-deploy runbook step, not a pre-merge claim.

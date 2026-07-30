# FRE-1060 — Make the recall selection gates visible in the admission record

**Ticket:** [FRE-1060](https://linear.app/frenchforest/issue/FRE-1060) (Approved, Urgent, Tier-1:Opus, `stream:build2`)
**Backing ADR:** ADR-0125 D3 item 5 (turn evidence contract) — this ticket restores the record's
completeness, which D3 item 5 already requires and FRE-1004 delivered only partially.
**Related:** FRE-1021, FRE-1041, FRE-1053 (all three reason about the ranked cap) · blocks FRE-1015.

---

## 1. The mechanism, located

`memory/proactive.py::build_proactive_suggestions` is the invisible gate. It returns **only the
survivors** (`ProactiveMemorySuggestions.candidates`), so the discarded population ceases to exist
*before* the request gateway ever sees it:

```
service.suggest_proactive_raw()            → 28 raw rows
build_proactive_suggestions():
  _dedupe_raw_by_turn_id()
  score each row; `if final < min_score: continue`   → 12 survive   (relevance threshold)
  scored.sort(desc); after_threshold = 12
  capped = scored[:max_candidates=10]                → 2 dropped    ← GATE, unnamed
  selection loop → 5 selected                        → 5 dropped    ← GATE, mislabelled
  log.info("proactive_memory_budget_trimmed", before=12, after=5, token_estimate=470, threshold=500)
  return ProactiveMemorySuggestions(candidates=selected)            ← the other 7 vanish here
context.py:249  → [c.payload for c in suggestions.candidates]       → 5 payloads
context.py:415  → build_recall_candidates(memory_context, ...)      → candidate_count = 5
```

The selection loop has **five** distinct exits, and the one log line names none of them:

| loop branch | config | what it means |
|---|---|---|
| `len(selected) >= max_injected_items` → break | 5 | the **ranked cap** — FRE-1021/1041/1053's top-5 race |
| `relevance_score < diminishing_score_floor` → break | 0.35 | absolute score floor |
| `prev_score - score > diminishing_score_gap` → break | 0.15 | diminishing-returns cliff |
| `est > max_tokens` → **continue** | 500 | one payload exceeds the whole budget |
| `token_budget + est > max_tokens` → break | 500 | the **token budget** |

On the melon turn `after=5` equals `max_injected_items=5` **and** `token_estimate=470` is within 30
of `threshold=500`. Either the ranked cap or the token budget could have decided, and the record
cannot say which. That is exactly AC-2's complaint, reproduced arithmetically.

### Scoping decision — the population is all 28 retrieved rows (owner call, 2026-07-30)

The first draft scoped the population to the 12 rows that cleared `min_score`, matching the ticket's
"7 of 12" arithmetic. Codex flagged that this recreates the same invisibility one layer down: a reader
still could not name *which* retrieved row the relevance gate removed. **The owner chose full
coverage** — every row `suggest_proactive_raw` returns is recorded, so no gate on the proactive path
is left unnameable.

That means two gates earlier than the first draft's boundary also need reasons:

- `_dedupe_raw_by_turn_id` (`proactive.py:174`) — a duplicate `turn_id`. Dropped before scoring, so
  its record carries `score=None`, which is truthful: no score was ever computed.
- `min_score` (`proactive.py:192`) — below the relevance threshold.

Cost is bounded and smaller than it looks: `RecallCandidateRecord` stores only kind, identity, score
and reason — **never the payload** — so 28 rows is ~28 small nested docs per capture rather than a
tripled document. `_build_payload_for_row` must now be called for sub-threshold rows too (today the
`continue` at line 192 skips it), which is dict construction with no I/O.

**What "complete" does and does not mean.** It covers every *discard* the proactive path performs. It
does not extend to the Neo4j query's own retrieval bound (`proactive_memory_vector_top_k`, and the
multipath arm limits) — a row never fetched is outside the record's reach, and no per-turn artifact
can name it. §2.5b's marker docstring says exactly this, so the marker cannot itself over-claim.

The log still reports `retrieved_row_count`, `deduped_row_count` and `scored_count` as three separate
figures so the counts corroborate the per-item records. `build_proactive_suggestions` currently
overwrites `raw_rows` with the deduped list, so the pre-dedupe count and rows must be captured before
line 174.

## 2. Design

### 2.1 `DropReason` gains one member per real gate

`captains_log/turn_evidence.py` — extend the existing enum rather than adding a parallel `gate`
field. `drop_reason` is already the field readers use, already mapped `keyword` in the ES template,
and already documented as "why it did not reach the final serialized model input". Eight new members,
one per discard the proactive path performs:

```
RECALL_DUPLICATE        # duplicate turn_id, dropped pre-scoring (score is None)
RECALL_SCORE_THRESHOLD  # below proactive_memory_min_score
RECALL_CANDIDATE_CAP    # scored[:max_candidates] — never reached selection
RECALL_ITEM_CAP         # max_injected_items — the ranked cap  ← AC-2 gate A
RECALL_TOKEN_BUDGET     # cumulative token budget exhausted    ← AC-2 gate B
RECALL_ITEM_OVERSIZED   # this one payload alone exceeds the budget
RECALL_SCORE_FLOOR      # below diminishing_score_floor
RECALL_SCORE_GAP        # diminishing-returns gap vs previous selected
```

Each maps 1:1 to a live branch. Naming fewer would repeat the sin the ticket condemns — reporting
one gate when a different one decided.

### 2.2 A candidate can arrive already dropped

`RecallCandidateRecord` gains `pre_drop_reason: DropReason | None = None`. `_resolve_admission`
short-circuits on it: `admitted=False, drop_reason=pre_drop_reason`, and — critically — it does
**not** decrement `rendered_budget`, because a pre-trim drop never reached the renderer. The
multiset-consumption logic for surviving candidates is untouched.

### 2.3 The discards are carried out of the proactive path

`memory/proactive_types.py`:

```python
class ProactiveMemoryDiscard(BaseModel):
    candidate: ProactiveMemoryCandidate
    drop_reason: DropReason

class ProactiveMemorySuggestions(BaseModel):
    candidates: list[ProactiveMemoryCandidate] = ...
    discarded: list[ProactiveMemoryDiscard] = Field(default_factory=list)   # new
    query_embedding_ms: float | None = ...
```

`turn_evidence.py` is deliberately free of `personal_agent` imports, so the import runs one way
only (`proactive_types` → `turn_evidence`). `proactive.py` already imports from `turn_evidence`, so
no cycle is introduced.

`build_proactive_suggestions` records, without changing which items are selected:

```python
retrieved = raw_rows                      # captured BEFORE the dedupe overwrites it
deduped = _dedupe_raw_by_turn_id(retrieved)
discarded = [Discard(row, RECALL_DUPLICATE) for row in <rows dropped by dedupe>]
# ... in the scoring loop, replacing the bare `continue`:
if final < cfg.proactive_memory_min_score:
    discarded.append(Discard(row, RECALL_SCORE_THRESHOLD, score=final))
    continue

capped = scored[:cap]
discarded += [Discard(c, RECALL_CANDIDATE_CAP) for c in scored[cap:]]

stop_index, stop_reason = None, None
oversized = []
for index, cand in enumerate(capped):
    if len(selected) >= max_injected_items:      stop_index, stop_reason = index, RECALL_ITEM_CAP;    break
    if score < diminishing_score_floor:          stop_index, stop_reason = index, RECALL_SCORE_FLOOR; break
    if prev is not None and prev - score > gap:   stop_index, stop_reason = index, RECALL_SCORE_GAP;   break
    est = _estimate_payload_tokens(cand.payload)
    if est > max_tokens:                          oversized.append(cand); continue
    if token_budget + est > max_tokens:           stop_index, stop_reason = index, RECALL_TOKEN_BUDGET; break
    selected.append(cand); token_budget += est; prev = cand.relevance_score

discarded += [Discard(c, RECALL_ITEM_OVERSIZED) for c in oversized]
if stop_reason is not None:
    discarded += [Discard(c, stop_reason) for c in capped[stop_index:]]
```

`_dedupe_raw_by_turn_id` changes to return `(kept, dropped)` — a private module function, so the
signature change is contained.

Oversized items are always at `index < stop_index` (they were `continue`d past), so
`capped[stop_index:]` cannot double-count them; with no terminal stop, every non-selected item inside
`capped` must have taken the oversized branch. Conservation holds across the whole path:
**`len(selected) + len(discarded) == len(retrieved)`**. `_estimate_payload_tokens` stays a
module-global call — `tests/personal_agent/memory/test_proactive.py:163` monkeypatches it by module
attribute.

**Only one terminal gate can fire per invocation** (codex finding 2). The loop `break`s on the first
terminal condition, so a single record carries at most one of
`RECALL_ITEM_CAP` / `RECALL_TOKEN_BUDGET` / `RECALL_SCORE_FLOOR` / `RECALL_SCORE_GAP`, alongside any
number of `RECALL_CANDIDATE_CAP` and `RECALL_ITEM_OVERSIZED` drops. AC-2's *"exhibiting one of each"*
is therefore **two records, not one** — the plan's original step-7 test was impossible to write
without fabricating attribution.

**Conservation is not an equality oracle** (codex finding 1, the critical one).
`len(selected) + len(discarded) == len(retrieved)` stays true even if a bug admits one more and
discards one fewer, so it cannot prove the failure clause. The existing token-budget test is likewise
permissive (`assert len(out.candidates) <= 1`, `test_proactive.py:169`). The real guard is a
**characterization oracle**: for each gate permutation, pin the *exact ordered list of selected
identities* produced by the pre-change code and assert equality. Conservation is kept as a secondary
check on the bookkeeping, not as the behavioural proof.

### 2.4 The log line stops misleading

Keep the event name `proactive_memory_budget_trimmed` (ADR-0128 governs the shared spine, not body
fields; a rename would break `docs/specs/PROACTIVE_MEMORY_DESIGN.md` and the eval README for no
gain) and add the honest fields: `stop_reason`, `discarded_by_gate` (a per-gate count map),
`retrieved_row_count`, `deduped_row_count`, `scored_count`. The name stays; the body now says which
gate fired — and the three separate counts stop dedupe losses being read as thresholding losses.

### 2.5 The discards reach the record

`request_gateway/context.py`:
- `_query_memory_for_intent` returns a 3-tuple `(memory_context, scores, discards)`, where
  `discards` is a `tuple[tuple[object, float | None, DropReason], ...]` of
  `(payload, score, reason)`. Triples rather than the Pydantic model so `turn_evidence` stays
  `personal_agent`-import-free. Non-proactive paths return `()`.
- **The all-dropped seam** (codex finding 3 — a real hole in the first draft). Today the proactive
  result is used *only* when it is non-empty (`context.py:240`), otherwise the function falls through
  to the entity-match path (`context.py:251`); the adapter explicitly permits an all-filtered result
  (`protocol_adapter.py:338`). So a turn where every candidate was discarded would lose the new
  records exactly when they matter most. Fix: bind `discards` from the proactive call into a local
  **before** the emptiness test, leave the fallback control flow byte-for-byte unchanged, and include
  `discards` in **every** return path below that point. Recall behaviour does not change; only the
  record does.
- `assemble_context` folds them in after the survivors:
  ```python
  recall_candidates = (
      *build_recall_candidates(memory_context, memory_scores),
      *build_discarded_candidates(discards),          # new, in turn_evidence.py
      *_session_fact_candidates(recall_context),
  )
  ```
  Position is irrelevant to correctness — pre-dropped candidates short-circuit and never touch
  `rendered_budget`.

**Ordering, stated rather than assumed** (codex finding 6). `items` becomes survivors in rank order,
then discards in rank order — *not* one globally rank-ordered list. Every item carries its `score`,
and `scored` was sorted by score descending, so exact global rank is recoverable by sorting. Recorded
in the `RecallAdmissionRecord` docstring, and `fre1021_entity_participation_census.py:50` — which
documents `candidate_kinds` as rank-ordered — is corrected. The census's own computations are
order-insensitive (`in`, `Counter`, `len`), so nothing breaks functionally.

`candidate_count` therefore becomes the true offered population (12 on the melon turn) and
`admitted_count` stays 5; **discarded = 12 − 5 = 7** is derivable, so no redundant count field.

### 2.5b The record says whether its own population is complete

Codex finding 4, and it overturns the first draft's call. Without a marker, a genuinely-untrimmed new
turn (5 candidates, 5 admitted, no drops) is **indistinguishable** from a legacy post-trim record with
the same shape — the record still cannot say whether it names the population or the survivors, which
is the ticket's own thesis recurring one layer up. Deploy timestamps are not a substitute for a
self-describing document.

`RecallAdmissionRecord` gains:

```python
candidate_population: Literal["offered", "post_selection"] = "post_selection"
```

`"offered"` — `items` names every candidate the selection stage considered, discards included.
`"post_selection"` — survivors only; the default, so **legacy documents read back correctly rather
than over-claiming**. New writes set `"offered"`. This is ADR-0125's *"absence must be explicit"*
applied to the record's own completeness.

It does **not** claim completeness of upstream *retrieval* limits (`min_score`, the dedupe, a query
`limit=`) — §1's named scope limit — and the field docstring says so, so the marker cannot itself
become an over-claim.

### 2.6 The resolver's success-path log (AC-4)

**Owner call, 2026-07-30:** log the resolved names in plaintext. `.claude/CLAUDE.md` forbids logging
PII and a personal-KG entity name can be PII, so this was put to the owner rather than decided here.
Plaintext is what AC-4 asks for and what makes the resolver's output diagnosable — counts alone
answer "did it run" but not "did it find the right entity", which is the question the melon
investigation actually needed. Consistent with `recall_admission.items[].identity`, which has stored
entity names in the same cluster since FRE-1004.

`memory/service.py::resolve_message_entity_names` — after `verify_mentions`, emit
`entity_mentions_resolved` with `trace_id`, `resolved_names`, `resolved_count`,
`fulltext_candidate_count`. **Logged unconditionally, including the empty result**: the ticket's
point is that silence is not evidence in either direction, so an empty resolution must be
distinguishable from the resolver not running. Entity names in telemetry is established practice on
this path (`recall_admission.items[].identity` already carries them).

### 2.7 ES template (additive, no type change)

`docker/elasticsearch/captains-captures-index-template.json` — the root is `dynamic: true` and
`drop_reason` is already an unrestricted `keyword` (line 149), so the six new enum values need **no
mapping change**. Two edits only: `candidate_population` added as `keyword` under
`recall_admission.properties`, and the `_meta.description` recording that `drop_reason` now spans
pre-selection gates and that `candidate_count` changed meaning at this deploy. **Additive, no type
change, no reindex, no back-attach** — the standing-approval deploy class.

## 3. Steps

| # | Step | Verify |
|---|---|---|
| 1 | **Characterization oracle first** (codex finding 1): `tests/personal_agent/memory/test_proactive_discards.py::TestSelectionUnchanged` — a table of gate permutations (item cap / token budget / score floor / score gap / oversized / candidate cap / none), each asserting the **exact ordered selected identities** against values captured from the pre-change code | passes on unmodified `proactive.py`; must stay green through every later step |
| 2 | Failing test: melon rows, no hint → `Melon` in `suggestions.discarded` with `RECALL_ITEM_CAP` | fails — `discarded` does not exist |
| 3 | `turn_evidence.py`: 8 new `DropReason` members, `pre_drop_reason` on `RecallCandidateRecord`, short-circuit in `_resolve_admission` (no `rendered_budget` decrement), `candidate_population` on `RecallAdmissionRecord`, new `build_discarded_candidates()` | module tests pass; legacy-doc test asserts the `"post_selection"` default |
| 4 | `proactive_types.py`: `ProactiveMemoryDiscard` + `discarded` field | step-2 progresses |
| 5 | `proactive.py`: `_dedupe_raw_by_turn_id` → `(kept, dropped)`; per-gate attribution for all eight reasons; honest log fields; `retrieved` captured before the dedupe | steps 1 **and** 2 pass |
| 6 | `context.py`: 3-tuple return, discards bound before the emptiness test, folded into `recall_candidates`, `candidate_population="offered"` | new gateway test: all-dropped proactive result still records the discards **and** still falls through to entity-match recall |
| 7 | `service.py`: `entity_mentions_resolved` log (AC-4) | new test asserts emission on a hit **and** on an empty resolution |
| 8 | AC-2 tests: one record exhibiting `RECALL_ITEM_CAP`, a second exhibiting `RECALL_TOKEN_BUDGET` (two records — they are mutually exclusive per §2.3) | both pass |
| 9 | Conservation test: `selected + discarded == retrieved` across the same permutations, including duplicate and sub-threshold rows (secondary check) | passes |
| 10 | ES template (`candidate_population` + `_meta`), `PROACTIVE_MEMORY_DESIGN.md`, census docstring: rank-order claim corrected + pre/post-deploy figures not comparable | `pre-commit run --all-files` |
| 11 | Step-8 quality gates + `/code-review high` + `/security-review` | all green |

## 4. Acceptance criteria

| AC | Criterion | How this plan proves it |
|---|---|---|
| 1 | On a live turn whose candidate population exceeds the budget, the record names every pre-trim candidate with kind, score and drop reason; discarded count non-zero and correct. **Live, not a fixture.** | Code delivers it; **proof is master's post-deploy step** — a live turn's `recall_admission` queried from `agent-captains-captures-*`. Unit tests assert the mechanism; the AC is satisfied only by the live record. Runbook in the handoff. |
| 2 | A reader can state, for a candidate that did not reach the model, whether the ranked cap or the token budget removed it — one of each exhibited | `RECALL_ITEM_CAP` vs `RECALL_TOKEN_BUDGET` on `drop_reason`; step-8 exhibits each in its own record (they are mutually exclusive within one invocation — §2.3); live query in the runbook |
| 3 | The melon-turn re-run names which gate discards the entity candidate, or shows none does | **Requires a live owner turn** — master's post-deploy step, not a build-session action (memory `feedback_never_fire_live_gateway_without_explicit_ok`). Step-1 test predicts `RECALL_ITEM_CAP`; the live record confirms or refutes. |
| 4 | The resolver emits a success-path log with resolved names + trace id, verified live | `entity_mentions_resolved`; unit test for emission, live ES query in the runbook |

**The failure clause:** *"It fails if the trim is made more generous without first being made
visible."* This plan changes **no threshold, cap, weight or budget** — `selected` is bit-for-bit
identical before and after. Only the record grows. **Step 1's characterization oracle is what pins
that**, written and green *before* any production edit; the conservation invariant (step 9) is a
bookkeeping check and would not have caught a survivor-set change on its own.

## 5. Out of scope

- Widening any gate (forbidden by the ticket until measurement lands).
- The two legacy executor recall paths (`executor.py:3655`/`3712`) — they never call
  `build_proactive_suggestions`, so they have no pre-trim population to lose.
- The Neo4j retrieval bound (`proactive_memory_vector_top_k`, multipath arm limits) — a row never
  fetched cannot be named by a per-turn record (§1).
- Backfilling historical captures. Legacy records keep `candidate_population="post_selection"`,
  which is the truthful reading of what they hold; the discarded items were never persisted and
  cannot be reconstructed (ADR-0125 Option 2: "recall identities cannot be reconstructed after the
  fact, because the ranking that produced them is not deterministic across index state changes").
- Reindexing `agent-captains-captures-*` (one additive `keyword`, no type change).

## 6. Codex plan-review disposition

Six findings, all verified against the code before acceptance.

| # | Codex finding | Disposition |
|---|---|---|
| 1 | Conservation invariant does not prove selection bit-identical — **Critical** | **Accepted.** Step 1 is now a characterization oracle on the exact ordered survivor list, written first. |
| 2 | `RECALL_ITEM_CAP` and `RECALL_TOKEN_BUDGET` cannot coexist in one record — **High** | **Accepted.** Verified: the loop `break`s on the first terminal gate. AC-2 is proven with two records; the original step-7 test was impossible. |
| 3 | All-dropped proactive path loses the discards and may alter the fallback — **High** | **Accepted.** §2.5 binds discards before the emptiness test at `context.py:240` and leaves fallback control flow unchanged. |
| 4 | No per-document version marker leaves legacy and new records indistinguishable — **Medium** | **Accepted, overturning the first draft.** §2.5b adds `candidate_population`, defaulting to `"post_selection"` so legacy docs do not over-claim. |
| 5 | Excluding sub-`min_score` rows recreates invisibility one layer down; `raw_row_count` conflates dedupe with thresholding — **Medium** | **Fully accepted — the owner escalated it.** Presented as a scope choice; the owner chose all 28 rows per-item, so `RECALL_DUPLICATE` and `RECALL_SCORE_THRESHOLD` were added and no proactive-path discard is left unnameable. The log reports three separate counts. |
| 6 | Survivor-first assembly breaks the documented rank-order property — **Low** | **Accepted as documented, not as reordered.** Verified the census's computations are order-insensitive; ordering is now stated in the model docstring and the census docstring corrected. Global rank stays recoverable via `score`. |

## 7. Found during implementation (folded in, per build SKILL §5)

- **The log guard had the same blind spot as the record.** `if len(selected) < after_threshold`
  cannot see the two gates upstream of scoring, so a turn whose only losses were duplicates or
  sub-threshold rows emitted **no event at all** — an observability hole in the exact mechanism this
  ticket closes, and one the owner's scope call (all 28 rows) exposed. Now `if discarded:`. A pure
  widening: the old condition implies the new one, so no previously-logged turn stops logging.
  Pinned by `TestTheTrimEventIsEmitted`.
- **`AssembledContext` is rebuilt by `apply_budget`**, which would have silently reset
  `candidate_population` to the conservative default on **every live turn** — the field would have
  shipped permanently dark. Carried explicitly (`budget.py`) and pinned by a test that also asserts
  it survives the trimming path, not just the pass-through.
- **`_discard` split into `_discard_row` / `_discard_candidate`.** The single helper took
  `row_or_payload` with an optional `kind`, needed two `type: ignore`s, and made the pre- vs
  post-scoring distinction a runtime branch instead of a signature. One `type: ignore` remains, on
  `_build_payload_for_row`'s `str` return narrowing to the payload `Literal` — pre-existing idiom at
  the sibling call site.
- **`RecallCandidateRecord` needed no `score` fabrication** for pre-scoring gates because
  `ProactiveMemoryDiscard.relevance_score` is nullable — the reason it is deliberately not a wrapper
  around `ProactiveMemoryCandidate`, whose `relevance_score` is a required `[0, 1]` float. Wrapping
  would have forced a fake 0.0 onto every duplicate.

## 8. Pre-existing failures on `origin/main` (not introduced here, verified)

Both confirmed against a pristine `origin/main` checkout, so neither is this branch's:

- `tests/personal_agent/memory/test_session_digest_read_live.py::test_round_trips_a_real_written_digest`
  — asserts `accepted is True` against a `SessionWriteResult.ACCEPTED` enum. Fails identically on a
  clean tree (verified by `git stash`). Not in this diff's blast radius.
**Correction on the second item, which was NOT pre-existing.** `check-identity-threaded` blocked the
commit, and the first diagnosis — "pre-existing, zero new findings" — was right about the *violation*
and wrong about the *failure*. `scripts/check_identity_threaded.py:377` keys its allowlist on
`(path, line)`, so the 15 lines added for `entity_mentions_resolved` shifted `service.py`'s
already-allowlisted `MERGE (e:Entity {name: $name})` from 2169 to 2184 and invalidated the entry.
Diffing findings main-vs-branch could not see this: the finding is identical, only its line number
moved. Fixed by updating the allowlist line, which is the file's documented convention — the entry's
own comment records FRE-1041's `+77` and FRE-1021's prior value. All hooks pass.

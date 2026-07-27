# FRE-1010 — Task-assist recall: per-kind rendering, and retiring the cap of 3

**Backing:** no ADR decides this branch (that is part of the defect). Related: ADR-0100
(relevance-bounded recall — the floor question), ADR-0125 D3 item 5 / FRE-1004 (the
record that made this visible), FRE-1002 (the truncation guard whose allowlist entry
this ticket retires).
**Branch:** `fre-1010-task-assist-recall-render`
**Observed defect:** trace 94b70cd9, 2026-07-27 — 5 candidates, 3 admitted, two described
ice-cream entities dropped at 0.562/0.560 against an admitted item at 0.563.

## The three defects, verified against the real code

All three live in `orchestrator/executor.py` `step_llm_call`, lines 4205–4227.

```python
if ctx.memory_context[0].get("type") in ("entity", "session"):   # (3) first item picks the branch
    ...
else:
    _rendered_memory_ids = tuple(
        memory_item_identity(mem)[1] for mem in ctx.memory_context[:3]   # (1) cap of 3
    )
    for i, mem in enumerate(ctx.memory_context[:3], 1):
        _ms += f"{i}. {mem.get('summary', mem.get('user_message', ''))[:150]}...\n"   # (2)
```

1. **Score-blind positional cap.** `[:3]`, a bare constant, applied *after* the upstream
   gates have already bounded the set.
2. **Entities cannot render here.** Reads `summary`, falls back to `user_message`. The
   entity payload every producer builds is `{type, name, entity_type, description,
   mention_count}` — it carries **neither** key, so `.get` falls through to `""` and the
   item renders as a bare numbered bullet with no content. Verified against
   `memory/proactive.py:141-150`, `request_gateway/context.py:124-131`, `:271-280`.
3. **Branch selection reads only the first item.** A mixed set is rendered wholesale by
   whichever branch its top-scoring item happens to select.

**A fourth, found while reading (not named in the ticket).** The entity renderer
(`_render_memory_section_with_ids`, line 2238) reads `m.get('mentions', 1)`, but three of
the four producers of an entity item write **`mention_count`**
(`memory/proactive.py:149`, `request_gateway/context.py:130`, `:278`; only
`executor.py:924`'s `_format_broad_recall` writes `mentions`). So gateway-sourced
entities always render the literal string *"(mentioned 1x)"* — a **fabricated** count
presented to the model as fact, on an evidence path. Folded in (see D3): this is the code
the task-assist set will newly flow through, so shipping the unified renderer without
fixing it would push a known-wrong claim onto a new path.

## D1 — What replaces the cap of 3: **nothing. Remove it, defer to the upstream gates.**

The ticket asks this be decided deliberately, not swapped for a different constant. Four
gates already bound this set *before* render, each verified:

| # | Gate | Where | Nature |
|---|---|---|---|
| 1 | `recall_similarity_floor` (ADR-0100 §4) | `memory/service.py:916, 3303, 3538, 3990, 4831` | relevance floor, **config-driven + embedder-calibrated** |
| 2 | query limits — 5 (entity-match), 20 (broad) | `request_gateway/context.py:253`, `:215` | cardinality |
| 3 | `proactive_memory_{min_score, max_candidates=10, max_injected_items=5, max_tokens=500}` | `memory/proactive.py:192, 213, 220, 228` | score floor + cardinality + **token budget** |
| 4 | `apply_budget` | `request_gateway/budget.py:270` | token budget; drops `memory_context` on overflow and records the dropped ids |

**The decisive argument.** `apply_budget` estimates memory tokens over the *entire*
`memory_context` list — `budget.py:107`: `estimate_tokens(" ".join(str(item) for item in
memory_context))`. The budget has therefore **already paid for every item in the set**.
The render-time `[:3]` bought no token saving whatsoever; it discarded content the budget
had already accounted for and let through. Removing it makes what the model receives
match what the budget already assumed — it does not unbound anything.

**Why not a render-time relevance floor** (ticket option a): it would be a *second* floor
on top of ADR-0100's, necessarily uncalibrated, in direct contradiction of ADR-0100 §4's
"config-driven and embedder-calibrated, **never hardcoded**". Decisively, scores are not
reliably available at this point: `executor.py:3542` and `:3599` build recall candidates
with an **empty** score map (`build_recall_candidates(ctx.memory_context, {})`), so a
render-time floor would see `None` on those paths and have to fail open (no-op) or fail
closed (drop everything) inconsistently by path.

**Why not a render-time token budget** (option b): duplicates gates 3 and 4. Two token
budgets that can disagree is a worse failure mode than one.

**Retained:** the entity list keeps its existing `[:15]` bound (broad recall can return
20). It is *not* the reported mechanism — the ticket confirms only 5 candidates existed —
and changing it is behaviour the ticket did not ask for. It is promoted from a bare
inline slice to a named constant with a comment, which answers the "undocumented
constant" complaint at zero behavioural risk. Drops remain recordable: `_rendered_memory_ids`
returns only what rendered, so candidates − rendered = dropped (ADR-0125 D3 item 5).

## D2 — How an entity renders: **per-item-kind dispatch, replacing first-item branch selection**

The ticket's deeper question — "whether one branch should render heterogeneous item kinds
at all, or whether the render should dispatch per item kind rather than per the type of
the first item in the set" — is answered: **dispatch per item kind**. Branch-per-first-item
is unsound by construction; any mixed set renders some of its items through a renderer
that cannot read their shape, which is exactly defect (2).

`_render_memory_section_with_ids(items)` becomes the single entry point for all kinds.
`step_llm_call` calls it once with the full `ctx.memory_context` — no branch, no slice.
Kind is resolved with `memory_item_identity` (`captains_log/turn_evidence.py`), already the
project's single definition of item identity/kind, rather than re-deriving `.get("type")`
a third time.

Both existing framings are preserved, grouped:

- **entities** → `## Your Memory Graph — Known Entities`, existing line shape, existing
  described-only filter (FRE-374 D1), existing 15 bound.
- **episodes** → `## Relevant Past Conversations`, existing numbered-line shape.
- **sessions** → today the entity branch drops them *silently* (its own comment says so:
  "the entity branch skips 'session' items"). Rendering them with their summary closes a
  silent kind-based drop — the same class of defect this ticket exists to fix. Minimal:
  one line per session with a non-blank summary.

An item whose kind yields no renderable content contributes **no line and no id** — the
existing described-only contract, generalised. That is what makes "admitted" honest on
this path.

## D3 — Folded-in supporting changes (build skill §5 — not separate tickets)

- **`mention_count` / `mentions` key mismatch** (the fourth defect above): read
  `mention_count` then `mentions`, and **omit the clause entirely when neither is present**
  rather than fabricating `1`. Absence stays absence.
- **`mark_truncated` on the episode summary** at 400 chars, replacing the raw `[:150]`.
  400 matches the bound `memory/proactive.py:138` already applies to that exact field, so
  it is a no-op for proactive episodes and a real bound for the others. Re-derived
  percentiles from FRE-1002 (N=1864 real captures): user_message p50=66, p90=151, **p99=400**
  — so 150 was cutting well inside the distribution and 400 clears p99.
- **Retire the FRE-1002 allowlist entry** for this exact site
  (`scripts/evidence_truncation_allowlist.yaml`). FRE-1002's handoff assigned its removal
  to this ticket; leaving it would let it drift.

## Explicitly out of scope (the ticket says so)

The ADR-0125 contract amendment — making `admitted` *require* non-empty rendered content —
is a contract change the ticket assigns to "whoever owns the next contract ticket".
This PR makes the condition true on this path behaviourally; it does not change the
contract's definition or its enforcement.

## Acceptance criteria (the ticket's HOW IT IS PROVEN, made testable)

| # | Criterion | Fails if |
|---|---|---|
| AC-1 | A mixed set with a described entity **beyond position 3**: the entity's description text appears in the assembled context | a described entity is dropped for position alone |
| AC-2 | No candidate is dropped for position alone (render emits every item the upstream gates admitted) | any item is cut by a render-time positional bound |
| AC-3 | An entity admitted by the evidence record rendered **non-empty** content | an entity renders as an empty bullet |
| AC-4 | `_rendered_memory_ids` names exactly the items that contributed content | the record reports admitted for an item that contributed no content |

## Test plan (TDD order)

`_render_memory_section_with_ids` is a pure function — the criteria are asserted directly
against it, plus one call-site test proving the executor passes the full context unsliced.

1. `tests/test_orchestrator/test_memory_section_render.py` (new):
   - mixed set, described entity at index 4 → its description text is in the section (AC-1)
   - 6-item set → all 6 render; no positional truncation (AC-2)
   - entity-only item renders `[TYPE] Name: description`, never an empty bullet (AC-3)
   - returned ids == ids of items that produced a line, for a set containing a
     blank-description entity that must be excluded from both (AC-4)
   - episode + entity + session in one set → all three framings present
   - `mention_count` renders the real count; `mentions` (legacy shape) also renders;
     neither present → no "(mentioned …)" clause at all
   - long episode summary → carries the `mark_truncated` marker, not a silent clip
2. Call-site: assert `step_llm_call` renders from the full `ctx.memory_context` (regression
   against reintroducing a slice) — reuse the existing executor test harness.
3. Existing suites that touch this path must stay green:
   `tests/test_orchestrator/test_prompt_layout_order.py`, `tests/personal_agent/orchestrator/test_turn_evidence_capture.py`,
   `tests/personal_agent/captains_log/test_turn_evidence.py`.
4. Guard: `uv run python scripts/check_evidence_truncation.py --strict src/personal_agent/`
   must exit 0 **with the allowlist entry deleted** — proving the site is genuinely fixed,
   not still exempted.

## Risk tier

**Standard/Complex** — `src/` logic on the assembled-context path, memory subsystem,
multi-file. Codex plan-review required before coding.

---

# Revision after codex plan-review (folded in before any code was written)

Codex reviewed the plan above (agent `ab1686ec9c3dc0f6b`) and returned one **blocker**,
two **highs**, one **medium** and two **lows**. Every finding was verified against source
before folding in; all six were correct. This section supersedes the sections above where
they disagree.

## Blocker — AC-2 contradicted the retained `[:15]` entity cap

As originally worded, AC-2 ("No candidate is dropped for position alone") is false while
`entity_items[:15]` survives (`executor.py:2232`). Broad recall can supply 20
(`request_gateway/context.py:215`), and the 16th-entity drop is **already deliberate,
documented and tested**: `tests/personal_agent/captains_log/test_turn_evidence.py:135-144`
(`test_rank_cap_drops_the_sixteenth_entity_distinguishably`).

**Resolution — narrow AC-2, do not remove the cap.** The ticket itself rules the 15-cap
out as the mechanism ("the rank cap of fifteen does not apply because there were only five
candidates"), so removing it is behaviour the ticket did not ask for, would break a
green test asserting the opposite, and would let broad recall's 20 full descriptions into
the prompt on the one path that has no token budget (see High-1). AC-2 is rescoped to the
`[:3]` task-assist cap that this ticket exists to fix.

> **Explicit non-goal:** entity items past position 15 remain positionally bounded. That
> bound is pre-existing, documented (FRE-374 D1), independently tested, and its drops are
> already recordable via `_rendered_memory_ids`. Master should read this as a stated
> non-goal, not an oversight.

## High-1 — D1's "the budget already paid for it" argument was overstated

**Verified false as a categorical claim.** `apply_budget` is called from exactly one place,
`request_gateway/pipeline.py:173` — it is a gateway-stage operation only. The executor's
legacy recall paths assign `ctx.memory_context` directly (`executor.py:3539` broad,
`:3563-3600` entity-match) and never invoke it.

**Corrected rationale** (the conclusion survives; the reasoning is narrowed):

- **Gateway path** — `apply_budget` runs at `pipeline.py:173` and its estimate covers the
  *whole* list (`budget.py:60-63`, `:103-108`), all-or-nothing (`budget.py:269-278`). The
  executor consumes that post-budget list (`executor.py:3274-3276`). Here the original
  claim holds exactly: the budget already accounted for every item, so `[:3]` bought no
  token saving and only lost content. This is the path the code itself calls "all of them"
  (`executor.py:3220-3221`).
- **Legacy path** — no token budget. Bounded by **cardinality** instead: broad recall
  `limit=20` (`executor.py:3531`), entity-match `limit=5` (`executor.py:3569-3574`), plus
  the surviving `[:15]` entity bound at render.

So: **no path is unbounded once `[:3]` is removed**, but not because every path is
token-budgeted — one is token-budgeted and the other is cardinality-bounded. The
"already paid for" sentence in D1 applies to the gateway path only and is corrected to say so.

## High-2 — the test plan could not actually establish AC-2 and AC-4

Pure-function tests over a synthetic item list prove the renderer's arithmetic, not
admission. ADR-0125 AC-3 requires comparison against the **final serialized model input**
(`ADR-0125:416-425`), and admission additionally requires the volatile block to reach the
wire (`turn_evidence.py:472-484`) — a render that emits ids whose block never lands is
recorded `ABSENT_FROM_FINAL_INPUT`, which a pure-function test cannot see.

**Resolution — assert the criteria at the executor seam.** The harness already exists:
`tests/personal_agent/orchestrator/test_turn_evidence_capture.py::TestEvidenceReachesTheCapture`
drives `step_llm_call` against a mocked LLM client and asserts on
`ctx.turn_evidence.recall.items` (admitted / `drop_reason`) and
`evidence.assembled_context.memory_identities` (`:152-178`). AC-1..AC-4 are asserted there,
against the real assembled system prompt captured from the mocked client's call args.
Pure-function tests over the renderer are retained as supplementary unit coverage, not as
the AC proof.

## Medium — session rendering is scope creep, and would only half-work

Two different session shapes exist and **only one carries a summary**:
`request_gateway/context.py:136-144` has `summary`; the legacy `_format_broad_recall`
(`executor.py:940-948`) emits `{type, session_id, dominant_entities, turn_count}` with **no
summary field at all**. Rendering "sessions" would therefore fix one producer and leave the
other still silently dropped — replacing a uniform silent drop with an inconsistent one.

**Resolution — sessions are an explicit non-goal.** They contribute no line and no id, so
the evidence record already shows them as dropped (candidates − rendered). Deciding what a
session should render — and reconciling the two shapes — is a separate design question this
ticket does not need to answer to fix the reported defect.

## Low — corrections folded in

- The fourth producer is `_format_broad_recall` (`executor.py:924`), not
  `_broad_to_memory_context`. Corrected above.
- `mention_count` repair refined: prefer `mention_count`, fall back to legacy `mentions`,
  render an explicit `0` when a producer supplied zero, and omit the clause **only** when
  neither key is present. (Previously the plan would have omitted a genuine zero.)

## Truncation: render the summary **whole**, do not re-truncate

Codex flagged that gateway entity-match episodes already carry up to 800 chars
(`request_gateway/context.py:302`, set by FRE-1002), so a render-time `mark_truncated(…, 400)`
would truncate an already-marked string and emit a **second** marker —
`"…[truncated 400 chars]…[truncated N chars]"`.

**Resolution — remove the render-time truncation entirely rather than marking it.** The
`summary` field is already bounded by its producers (proactive 400, gateway entity-match
800), and ADR-0125 D5 prefers *stored whole* over *shortened with a marker*. This also
retires the FRE-1002 allowlist entry by **deleting the truncation**, not by exempting or
marking it — a strictly better outcome for that guard. Any bound that is genuinely needed
belongs upstream where the token budget lives, not re-applied at render.

## Additional constraint found in review: the section header is load-bearing

`orchestrator/sub_agent.py:30-33` defines `_MEMORY_CONTEXT_MARKER = "## Your Memory Graph"`
and scans primary context for it to answer FRE-505's "was memory/KG in the sub-agent's
input?". The unified renderer **must keep that exact header string**; a test pins it.

## Revised acceptance criteria

| # | Criterion | Asserted where |
|---|---|---|
| AC-1 | Mixed set, described entity **beyond position 3** → its description text appears in the assembled system prompt | executor seam (`step_llm_call`) |
| AC-2 | No item is dropped by a **task-assist positional cap** — every episode/entity the upstream gates admitted renders (entity `[:15]` bound is an explicit non-goal) | executor seam |
| AC-3 | An entity admitted by the evidence record rendered **non-empty** content — never a bare numbered bullet | executor seam |
| AC-4 | `evidence.recall.items` admitted set == exactly the items that contributed content; blank-description entity is `NOT_RENDERED` | executor seam |
| AC-5 | `"## Your Memory Graph"` header preserved (FRE-505 sub-agent marker) | unit |

## Revised test plan

1. **Executor seam** — extend `tests/personal_agent/orchestrator/test_turn_evidence_capture.py`
   (or a sibling using the same `_run` harness): mixed episode+entity set with a described
   entity at index 4; assert its description text is in the system prompt sent to the mocked
   client (AC-1), all items admitted (AC-2), no empty bullet (AC-3), and
   `evidence.recall.items` admitted == content-contributing items with the blank-description
   entity `NOT_RENDERED` (AC-4).
2. **Unit** — `_render_memory_section_with_ids` directly: per-kind dispatch, `mention_count`
   vs `mentions` vs neither, blank-description exclusion, header string (AC-5), summary
   rendered whole (no truncation marker introduced).
3. **Regression** — must stay green unchanged:
   `tests/personal_agent/captains_log/test_turn_evidence.py` (incl. the 16th-entity cap test),
   `tests/personal_agent/orchestrator/test_memory_render_filter.py`,
   `tests/test_orchestrator/test_prompt_layout_order.py`.
4. **Guard** — `uv run python scripts/check_evidence_truncation.py --strict src/personal_agent/`
   exits 0 **with the FRE-1002 allowlist entry deleted**, proving the site is fixed rather
   than still exempted.

---

# Owner condition (approved 2026-07-27, 15:01Z) — bound the unbudgeted path by construction

## Retired concern: prompt-cache erosion. There is none.

Master verified and it is recorded here so the next reader does not re-run the
investigation. `executor.py` ~4195 (ADR-0081 D1) builds `memory_section` **without
injecting it**; `_inline_volatile_into_last_user_message` (`executor.py:1322`) appends it
as the volatile tail of the **last user turn**, in the documented gradient (skill bodies →
recalled memory → salient highlights → artifact-builder note). `_apply_anthropic_cache_control`
marks three breakpoints — end of system message, last tool definition, and (frozen layout)
the last frozen message before the current user turn — **all of which precede the volatile
tail**. Changing this section's size or content therefore cannot invalidate the cached
prefix.

**AC-1 wording corrected accordingly:** the description text lands in the assembled
**input, in the user turn** — *not* in the system prompt. The earlier wording is what
raised the false alarm.

## The real issue: cost, not caching

Because the volatile tail is deliberately **outside** the cache, every character added
there is a **fresh input token on every turn, never amortised**. The gateway-path argument
is unaffected (`apply_budget` already estimated over the whole list and is all-or-nothing,
so `[:3]` bought zero saving). The **legacy path has no token budget** — and the
un-revised plan removed the 150-char re-truncation while leaving per-item length unbounded.

The plan already reasoned about exactly this hazard when it kept the entity `[:15]` cap,
then left the **episode** side of the same unbudgeted path unbounded. That inconsistency
is the condition of approval.

**Where the exposure actually is** (verified): on the legacy episode path cardinality is
*already* bounded at 5 (`executor.py:3572`, `limit=5`) — every episode producer caps at 5
(gateway proactive `max_injected_items=5`, gateway entity-match `limit=5`). The unbounded
dimension is **per-item summary length**: `"summary": conv.summary or mark_truncated(conv.user_message, 400)`
(`executor.py:3593`), where `conv.summary` is a **stored digest with no render-time bound**
once `[:150]` is removed.

## The bound, stated

Two named constants, applied by construction at render:

| Constant | Value | Why this number |
|---|---|---|
| `_MAX_RENDERED_EPISODES` | **5** | Restates the cardinality every episode producer already enforces upstream, as a render-time bound. Cannot bite on any current path — it is defence in depth against a future producer, not a new cap. |
| `_MAX_ITEM_CHARS` | **1000** | Bounds an unbounded stored digest / entity description. Chosen **above** the largest upstream value (gateway entity-match writes `mark_truncated(…, 800)`, so ≤ 800 + ~26 marker chars ≈ 826) so it never double-truncates or emits a second marker. ≈ ADR-0124's ~250-token digest target. |

Applied via `mark_truncated`, so any bound that does bite is marked, not silent (ADR-0125 D5).

**Applied to entity descriptions too, not only episodes.** Bounding one side and leaving
the other is precisely the inconsistency this condition exists to correct — and entity
descriptions are the *larger* exposure, since the entity branch has **never** truncated
them.

**Worst case on the unbudgeted legacy path, stated:**

| | episodes | entities | total |
|---|---|---|---|
| today | 3 × 150 = 450 | 15 × **unbounded** | unbounded |
| un-revised plan | 5 × **unbounded** | 15 × **unbounded** | unbounded |
| **as built** | 5 × 1000 = 5,000 | 15 × 1000 = 15,000 | **≤ ~20,000 chars ≈ ~5,000 tokens** |

So this is a *reduction* in worst case against both today and the un-revised plan — today's
entity side was already unbounded. The episode side rises from 450 to ≤5,000 chars, which
is the intended effect of the ticket (the model was being starved), now bounded and stated
rather than open-ended.

**Bounded by construction rather than shipped-and-watched**, because the instrument that
would catch erosion is broken: **FRE-1008** established that the static-prefix and dynamic
prompt hashes are computed from the same input, so ADR-0078 cache-erosion measurement
cannot detect erosion at all.

## Deploy posture

Deploys are **HELD for a batched deploy as of 2026-07-27 14:00Z**. The handoff runbook must
say **batched deploy pending** rather than implying anything takes effect at merge.

---

# Self-review fixes (pre-PR, high-effort review of the branch diff)

A high-effort code review of the diff returned **no findings at or above the confidence
bar**. Three sub-threshold observations were raised; two were fixed on-branch because they
are the same *class* of defect this ticket exists to fix (content silently lost on the
recall path), and one was deliberately not fixed.

**Fixed — bounds now cap rendered content, not candidates considered.** The first draft
applied `episodes[:_MAX_RENDERED_EPISODES]` *then* filtered for content. Blank leading
items could therefore consume slots and silently exclude a later item that did have
content — "recalled then discarded", precisely the shape this ticket fixes, and it
contradicted the bound's own stated purpose (bounding the volatile tail's *cost*, to which
only rendered content contributes). Now filters first, then bounds. Applied to entities
as well as episodes for consistency; the stated worst case is unchanged (the bound still
caps at 15 / 5 rendered items). Regression: `test_blank_items_do_not_consume_bound_slots`.

**Fixed — whitespace-only summary no longer suppresses the user message.**
`(summary or user_message or "")` treats `" "` as truthy, so a whitespace-only summary
rendered as empty (and the item was dropped) even though `user_message` held real content.
Each candidate is now stripped before the fallback. Regression:
`test_whitespace_only_summary_falls_back_to_user_message`.

**Not fixed, flagged for the contract ticket — duplicate-identity admission matching.**
`_resolve_admission` (`captains_log/turn_evidence.py`) matches rendered identities against
candidates with a `Counter`, and its own docstring assumes the renderer emits an
*order-preserving prefix* of same-identity candidates. This renderer does filter-then-bound,
which is a **subsequence**, not a strict prefix. If two memory-context items ever shared an
identity and only the later rendered, admission could be attributed to the wrong instance.
Deliberately not changed here: it is ADR-0125 contract logic, and this ticket's own body
assigns contract changes to "whoever owns the next contract ticket". Mitigated in practice
by `_dedupe_raw_by_turn_id` upstream and by the rarity of duplicate identities in one recall
batch. Raised in the handoff so the contract ticket inherits it rather than rediscovering it.

**One test retired by this work.** `tests/scripts/test_check_evidence_truncation.py::test_real_executor_fre_1010_site_is_flagged_when_strict`
asserted that FRE-1002's guard *flagged* this render site with the allowlist bypassed — its
proof the guard caught real code, not only fixtures. That subject no longer exists. The test
was **inverted rather than deleted** (`test_real_executor_is_clean_with_no_allowlist_entry`):
as a real-tree assertion with an empty allowlist it now fails if the clip is ever
reintroduced, which is the regression that matters now.

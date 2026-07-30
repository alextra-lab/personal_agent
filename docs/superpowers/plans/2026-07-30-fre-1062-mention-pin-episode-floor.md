# FRE-1062 — Mentioned-entity pin + episode floor in proactive selection

**Ticket:** FRE-1062 (Approved by owner in-session 2026-07-30, Tier-1:Opus).
**Trigger:** first live post-FRE-1061 melon turn (trace `4eca4070`): the literally-mentioned
`Melon` ranked 8th and was `recall_item_cap`-cut behind unmentioned associates; the answer's
substance rode the single admitted episode (94b70cd9), which survived by rank luck.
**Design stance:** stated rules, no new score constants. **Revised after codex plan-review
(three blocking findings, all incorporated — see §Revisions).**

## The two rules (final form)

1. **Episode floor (first in the walk).** When ≥1 episode candidate cleared `min_score`
   AND the *existing* `diminishing_score_floor` (the system's own quality bar — no new
   constant), the best-ranked such episode is admitted first, so no combination of pins
   and entities can starve it of item slots or token budget. Rationale: the live turn
   demonstrated episodes carry the answer's substance; under the FRE-1061 pair split they
   are the kind at structural risk of crowd-out.
2. **Mentioned-entity pin (next).** Entity candidates whose `name` is among the message's
   graph-resolved literal mentions (FRE-1041 resolver output) are admitted next — score
   order, bounded by `_MENTIONED_ENTITY_PIN_LIMIT = 2` (module constant), still subject
   to `min_score`, the token budget, and the oversize skip. Pins bypass only the rank
   caps and the diminishing floor/gap heuristics — the literal mention is the
   justification for that bypass; it is the strongest relevance signal the system
   receives and today buys ≤ ~0.08 of final score.

**Ordering note.** Walk order is *admission priority*, not presentation: the renderer
partitions memory items by kind into separate sections, so putting the floor episode
ahead of pins changes which items survive the budget, never how they read to the model.
This deliberately amends FRE-1061's "entity ahead of its sibling" property from a rank
claim to a membership claim; the affected FRE-1061 assertions are updated with in-test
rationale (they pinned tie-break mechanics, not product behaviour).

## Construction (codex findings 2–4: explicit, capacity-safe, identity-deduped)

In `build_proactive_suggestions`, after scoring + stable sort (`scored_sorted`, all
≥ `min_score`):

```
floor    = first episode in scored_sorted with score ≥ diminishing_score_floor  (or None)
pins     = first ≤2 entity candidates with name ∈ mentioned_entity_names,
           excluding floor's identity (floor is an episode, so disjoint by kind)
head     = ([floor] if floor else []) + pins
rest     = [c for c in scored_sorted if _candidate_identity(c) not in head_identities]
walk     = (head + rest)[:max_candidates]          # ONE capacity-safe window
overflow = (head + rest)[max_candidates:]          # → RECALL_CANDIDATE_CAP, as today
```

- `head` is truncated with everything else by the single `[:max_candidates]` bound, so a
  legal `max_candidates=1` config cannot go negative and the total window bound holds.
- The selection loop runs over `walk` with the existing gates. Item cap and token budget
  apply to every item (head included). The diminishing floor/gap terminal checks apply
  **only in the rest region** (`index >= len(head)`); `prev_score` is tracked from
  rest-region admissions only, starting `None` (first rest item: gap cannot fire, floor
  can — with an empty head this is exactly today's loop, which is AC-4).
- Terminal-stop attribution: `walk[stop_index:]`, i.e. the explicit walk list, never the
  old `capped` indexing — this is what keeps FRE-1060 attribution correct under
  reordering. An oversized head item is stepped over (`RECALL_ITEM_OVERSIZED`) with no
  cascading re-reservation (documented; a second reservation pass would re-open the
  double-count risk).
- Conservation: threshold discards + overflow (candidate cap) + walk outcomes
  (selected / oversized / terminal tail) still partition the deduplicated candidates.
- Trim event: additive `pinned_mention_count` (pins admitted) + `episode_floor_applied`
  (floor admitted) fields.

## Threading (codex finding 6)

`mentioned_entity_names: Sequence[str] | None = None` appended to
`protocol.py::suggest_relevant` → `protocol_adapter.py` → `build_proactive_suggestions`;
`context.py` passes the FRE-1041 `entity_names` verbatim. The `session_entity_names`
overlap merge is untouched. Also: update `FakeMemory.suggest_relevant`'s signature in
`test_protocol.py` (runtime protocol checks see only method presence) and extend
`test_proactive_path_passes_resolved_names` (`test_context.py`) to assert
`mentioned_entity_names` receives the resolver output distinct from the overlap merge.

## Revisions from codex plan-review

1. *Blocking:* pins could starve the floor episode via token budget or a legal
   `max_injected_items=2` → **floor moved ahead of pins**; guarantee now real.
2. *Blocking:* reordered-tail attribution unspecified → **explicit `walk` list; terminal
   tail = `walk[stop_index:]`**; single `[:max_candidates]` window replaces the
   subtraction bookkeeping.
3. *Blocking:* unconditional episode promotion lacked a quality guard and the FRE-1061
   symmetry claim was inaccurate → **floor episode must clear `diminishing_score_floor`**
   (existing constant); symmetry rationale dropped in favour of the demonstrated
   substance-carrier argument; three FRE-1061-era rank-order assertions updated to
   membership/set assertions with in-test rationale
   (`test_the_top_named_row_puts_its_entity_at_rank_one`,
   `test_score_combination_non_empty`, `test_diminishing_score_gap` order).
4. Identity-deduped head (floor cannot reappear in rest; pin cannot double-admit) + a
   floor-identity-appears-exactly-once test.
5. All-episode FRE-1060 oracle verified case-by-case unchanged: rank-1 episode is
   already first when it clears the floor; when it does not (0.330 < 0.35 case), no
   floor is reserved and the walk equals today's — both directions preserved.

## Tests (new file `tests/personal_agent/memory/test_proactive_selection_rules.py`)

- AC-1: mentioned entity ranked past the item cap by unmentioned associates → admitted;
  the displaced candidate's drop is `recall_item_cap`.
- AC-2: top ranks all entities → best qualifying episode still admitted (floor), first.
- AC-3: sub-`min_score` mention not pinned; oversized pin stepped over
  (`recall_item_oversized`) while the floor episode and rest admit.
- AC-4: no mentions → today's selection except the floor; empty-head fixture asserts
  byte-identical selection to the pre-change loop.
- AC-5: conservation + (kind, identity) disjointness with pins and floor active; trim
  event carries both new fields; floor identity appears exactly once across
  admitted+discarded.
- Bounds: 3 resolved mentions → exactly 2 pins; `max_candidates=1` → window holds the
  floor episode only, no negative slicing; pin starvation case from codex (two 240-token
  pins, 500 budget, 30-token episode) → episode admitted BEFORE pins consume budget.
- Sub-floor episode population (all episodes < diminishing floor) → no reservation,
  today's floor-stop behaviour.

## Test commands

```
make test-file FILE=tests/personal_agent/memory/test_proactive_selection_rules.py
make test-file FILE=tests/personal_agent/memory/test_proactive_entity_split.py
make test-file FILE=tests/personal_agent/memory/test_proactive_discards.py
make test && make mypy && make ruff-check && make ruff-format
```

Expected: new tests red before implementation; FRE-1060 oracle green untouched; three
named FRE-1061-era assertions updated (rank→membership) with in-test rationale.

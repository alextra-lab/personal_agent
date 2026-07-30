# FRE-1061 — Proactive recall: emit entity AND episode candidates per retrieved row

**Ticket:** FRE-1061 (Approved by owner in-session 2026-07-30, Tier-1:Opus, PersonalAgent).
**Backs:** the entity-recall root cause found 2026-07-30
(`telemetry/entity_recall_findings_explore_2026-07-30.md`, answering
`telemetry/entity_recall_investigation_brief_2026-07-30.md`). Restores the premise
ADR-0126's acceptance criteria stand on; completes the FRE-1021 → FRE-1041 → FRE-1060
lineage.
**Specs:** ADR-0039 · `docs/specs/PROACTIVE_MEMORY_DESIGN.md` (line 38 lists `entity` as a
first-class candidate kind — this plan implements what that line already promises).

## Root cause being fixed (verified against source and the production graph)

`memory/proactive.py::_build_payload_for_row` converts any raw row that carries a
cross-session turn with text into an **episode** payload, discarding the anchoring
entity's `name`/`entity_type`/`description`. 7,442 of 7,446 production entities carry such
a turn, so the `entity` branch is reachable for 0.05 % of the graph, and
`_dedupe_raw_by_turn_id` additionally drops whole entities whose best turn collides with a
higher-ranked entity's (29→13 on the melon turn). Combined with the early return at
`request_gateway/context.py:301`, no entity that the owner has ever discussed
cross-session can reach the model through the automatic path.

## Design

One raw row is an *(entity, best-turn)* **pair** — two memories, not one. Split it:

1. **`_split_row_payloads(row) -> list[tuple[str, dict[str, Any]]]`** replaces
   `_build_payload_for_row`:
   - `("entity", {...})` when `row["name"]` is truthy — same payload shape the existing
     `entity` branch builds (name, entity_type, description, mention_count).
   - `("episode", {...})` when `turn_id and (user_message is not None or summary)` — the
     existing episode payload, unchanged (incl. FRE-1004 `conversation_id` and the
     ADR-0125 D5 `mark_truncated` fallback).
   - Legacy fallback: a row with neither yields the old `name="unknown"` entity payload so
     no row silently vanishes from the accounting.
   - **Order: entity first.** `sort` is stable, so at equal scores (the pair shares its
     row's score) the entity precedes its sibling episode. On production dense rows —
     which carry `node.name` — the top-scored *named* row therefore contributes an entity
     at rank 1. This is a tie-break, **not an admission guarantee** (codex plan-review):
     a nameless row yields no entity, an oversized entity payload is stepped over, and
     the renderer still drops blank-description entities. AC-2 is asserted at the
     proactive-selection altitude (offered + admitted within proactive caps on the
     fixture), not end-to-end render.
2. **Kind-appropriate dedupe** replaces `_dedupe_raw_by_turn_id`: while flattening rows
   into `(kind, payload, row)` items (vector order best-first), collapse episodes on
   `conversation_id` and entities on `name`. A collapse is still not a discard (owner
   call 2026-07-30 stands — identity is preserved in the kept item). Distinct entities
   sharing a best turn now each survive as entity candidates (AC-3); only the shared
   episode collapses.
3. **Score per item, from its row** — subscores exactly as today (`vector_score`,
   overlap over `[name, *key_entities]`, recency from the row timestamp, topic). The pair
   shares one score by construction. All eight FRE-1060 gates operate unchanged on the
   item list; threshold discards now use the item's own (kind, payload) instead of
   re-deriving via `_discard_row` (which, with `_dedupe_raw_by_turn_id`, becomes unused —
   both removed as orphans of this change).
4. **Telemetry (revised per codex plan-review — never repurpose a field's unit):**
   `proactive_memory_budget_trimmed` keeps its guard; `retrieved_row_count` stays the
   raw-row count; `deduped_row_count` is **removed** (row-level dedupe no longer exists
   as a concept) and two honestly named fields replace it: `split_candidate_count`
   (after the pair split, before dedupe) and `deduped_candidate_count` (after
   kind-appropriate dedupe). Consumers found by codex: the FRE-1060 tests (don't read
   these fields), `EVAL-proactive-memory/README.md` and `PROACTIVE_MEMORY_DESIGN.md`
   (both updated in this PR). The FRE-1060 conservation contract restates over
   candidates: emitted + discarded == deduplicated candidates.
   **Census discriminator:** `scripts/audit/fre1021_entity_participation_census.py`
   gains an `--after <ISO>` boundary filter so the pre/post-FRE-1061 candidate universes
   are never averaged together; master records the deploy timestamp in the runbook. (The
   in-record schema-version alternative was rejected: it touches the evidence model + ES
   template for the same effect.)
   **Accepted residual risk (documented, not fixed):** downstream evidence identity is an
   unqualified string, so an entity literally named a turn-UUID would collide with that
   episode's identity. Dedupe here uses kind-qualified sets and the new invariant test
   asserts over `(kind, identity)`; the pathological cross-kind collision is called out
   in the PR for master's judgment rather than reworking the whole evidence contract.
5. **No changes** to `suggest_proactive_raw`, the lexical augment, `context.py` control
   flow (the early return is now *correct*: proactive itself carries entities — decided,
   documented in the module docstring), the renderer (already handles both kinds;
   `_MAX_RENDERED_ENTITIES=15` ample), or `turn_evidence.memory_item_identity`
   (entity → name already).

**Deliberate consequence:** with pairs, the candidate cap (10) and item cap (5) now cover
~half as many distinct rows. On the melon shape the admitted set becomes
entity/episode/entity/episode/entity instead of five episodes. That is the point of the
ticket; if the balance needs tuning it is a settings change, not a code change.

## Steps

1. **Tests first** (`tests/personal_agent/memory/test_proactive.py` + new
   `test_proactive_entity_split.py`):
   - AC-1: a melon-shaped row (name + cross-session turn with text) yields both kinds via
     `_split_row_payloads`; entity payload keeps name/type/description; episode payload
     unchanged (conversation_id, summary fallback).
   - AC-2: `build_proactive_suggestions` on a melon-shaped fixture under deployed
     settings admits ≥1 `kind=entity` candidate naming the entity; rank 1 is the entity.
   - AC-3: two named rows sharing `turn_id` → two entity candidates + one episode.
   - AC-4: conservation — emitted + discarded == deduplicated candidates, mixed-kind
     fixtures.
   - AC-5: gate attribution unchanged — reuse a mixed fixture and assert
     `RECALL_ITEM_CAP` etc. still name drops of either kind.
   - Edge cases (codex findings): nameless row → episode only; named turnless row →
     entity only; named row whose turn has no text → entity only; oversized entity +
     fitting episode → `RECALL_ITEM_OVERSIZED` on the entity, episode survives;
     duplicate names AND duplicate turns in one fixture; no-`(kind, identity)`-in-both-
     sets invariant over mixed kinds.
   - Update the three existing assertions the split legitimately changes
     (`test_score_combination_non_empty` 1→2 candidates;
     `test_dedupe_same_turn` 1→3 with kind breakdown;
     `test_episode_payload_marks_long_user_message_fallback_summary` re-pointed at
     `_split_row_payloads`). `test_proactive_discards.py` and
     `test_proactive_melon_regression.py` fixtures are nameless-episode/turnless-entity
     shaped and stay green untouched (verified by reading them); a short note added to the
     melon-regression module docstring pointing at FRE-1061 for the production mechanism.
2. Implement in `memory/proactive.py` (only file with logic changes).
3. Docs: `PROACTIVE_MEMORY_DESIGN.md` dedupe/accounting note; module docstrings.
4. Gates: `make test` (module, then full) · `make mypy` · `make ruff-check` +
   `make ruff-format` · `pre-commit run --all-files`.
5. Self-review: code-review skill at **high** (src/memory logic); security-review not
   applicable (no inputs/subprocess/auth/network surface touched) — state so in the PR.
6. PR + Linear handoff comment incl. **related-ticket guidance for master** (FRE-1015
   unhold; FRE-1041/1053/1054 status; ADR-0126 AC precondition now satisfiable; census
   step-change expectation at deploy).

## Exact test commands

```
make test-file FILE=tests/personal_agent/memory/test_proactive.py
make test-file FILE=tests/personal_agent/memory/test_proactive_entity_split.py
make test-file FILE=tests/personal_agent/memory/test_proactive_discards.py
make test-file FILE=tests/personal_agent/memory/test_proactive_melon_regression.py
make test && make mypy && make ruff-check && make ruff-format
```

Expected: new tests fail before the implementation (entity candidate absent), pass after;
the two FRE-1060 suites pass before AND after (their fixtures don't exercise the split).
